"""
Determina il ResponseType in modo deterministico, a partire da
(intent, SystemResult, step raggiunto).

Coerente col principio "l'AI interpreta/comunica, Python decide":
anche QUALE tipo di risposta dare non e' una decisione dell'AI, ma di
questo resolver. AI#2 riceve il response_type gia' stabilito e si
limita a scrivere il testo adatto.
"""

from __future__ import annotations

from app.context.models import (
    ConversationContext,
    ConversationStep,
    Intent,
    OperationType,
    ResponseType,
    SystemResult,
)

_STEP_TO_RESPONSE_TYPE = {
    ConversationStep.WAITING_FOR_SLOT: ResponseType.SHOW_AVAILABILITY,
    ConversationStep.COLLECTING_CUSTOMER_DATA: ResponseType.ASK_CUSTOMER_DATA,
    ConversationStep.WAITING_FOR_CONFIRMATION: ResponseType.ASK_CONFIRMATION,
}

# Step COMPLETED: il tipo dipende da QUALE operazione si e' conclusa.
# Aggiungere RESCHEDULE/CANCEL in futuro significa solo aggiungere qui
# la riga corrispondente - nessun'altra modifica.
_COMPLETED_TO_RESPONSE_TYPE = {
    OperationType.CREATE: ResponseType.BOOKING_CONFIRMED,
    OperationType.RESCHEDULE: ResponseType.RESCHEDULE_CONFIRMED,
    OperationType.CANCEL: ResponseType.CANCELLATION_CONFIRMED,
    OperationType.CHECK: ResponseType.APPOINTMENT_DETAILS,
}


def resolve_response_type(
    intent: Intent,
    system_result: SystemResult,
    context: ConversationContext,
) -> ResponseType:
    # Intent "di chiacchiera": non passano mai dal Router/dalla business
    # logic, quindi vanno riconosciuti qui prima di guardare il SystemResult.
    if intent == Intent.GREETING:
        return ResponseType.GREETING
    if intent == Intent.THANKS:
        return ResponseType.GOODBYE
    if intent == Intent.ASK_INFORMATION:
        return ResponseType.INFORMATION

    if not system_result.success:
        if system_result.error_code == "UNHANDLED_STATE":
            return ResponseType.ASK_CLARIFICATION
        return ResponseType.ERROR

    step = context.conversation.current_step

    if step == ConversationStep.COMPLETED:
        return _COMPLETED_TO_RESPONSE_TYPE.get(context.operation.type, ResponseType.INFORMATION)

    return _STEP_TO_RESPONSE_TYPE.get(step, ResponseType.INFORMATION)
