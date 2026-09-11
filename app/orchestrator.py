"""
ORCHESTRATOR

Collega, in un'unica funzione, il flusso lineare discusso:

  USER -> AI#1 -> Context Manager -> Router -> Business Logic
        -> SystemResult -> response_type_resolver -> AI#2 -> USER

Isolato: non sostituisce (ancora) il webhook reale in main.py. Serve a
dimostrare e testare che tutti i pezzi nuovi si incastrino correttamente
prima del collegamento definitivo.
"""

from __future__ import annotations

from app.ai.context_summary import build_ai1_input
from app.ai.format_helpers import format_appointment_target_question, time_of_day_greeting
from app.ai.interpreter import run_ai1_interpreter
from app.ai.responder import compose_final_message, run_ai2_responder
from app.ai.response_type_resolver import resolve_response_type
from app.context.context_manager import apply_ai1_result
from app.context.models import ConversationContext, ConversationStatus, Intent, Message, ResponseType, SystemResult
from app.router.intent_router import route

# Intent che non richiedono alcuna business logic / accesso al DB:
# saltano il Router e vanno dritti alla risposta.
_CHITCHAT_INTENTS = {Intent.GREETING, Intent.THANKS, Intent.ASK_INFORMATION}


def handle_message(
    context: ConversationContext,
    message_text: str,
    tenant: dict,
    knowledge: dict,
    knowledge_texts: dict | None = None,
) -> tuple[ConversationContext, str]:
    # È il primo messaggio di questa conversazione? Va controllato PRIMA
    # di apply_ai1_result, che porta lo status da NEW ad ACTIVE.
    is_first_message = context.conversation.status == ConversationStatus.NEW

    # 1. AI#1: interpreta (solo il blocco minimo, non l'intero context)
    ai1 = run_ai1_interpreter(message_text, build_ai1_input(context), tenant.get("timezone"))

    # 2. Context Manager: aggiorna lo stato
    context = apply_ai1_result(context, ai1, message_text)

    # 3. Router: decide ed esegue la business logic (skip per le chiacchiere)
    if ai1.needs_clarification:
        system_result = SystemResult(success=False, error_code="NEEDS_CLARIFICATION")
    elif ai1.intent in _CHITCHAT_INTENTS:
        system_result = SystemResult(success=True)
    else:
        context, system_result = route(context, tenant=tenant, knowledge=knowledge)

    # 4. Determina il tipo di risposta (deterministico, non lo decide l'AI)
    response_type = resolve_response_type(ai1.intent, system_result, context)
    if ai1.needs_clarification:
        response_type = ResponseType.ASK_CLARIFICATION

    # 5. Scrittura del messaggio finale.
    # CONFIRM_APPOINTMENT_TARGET è un caso speciale: la domanda contiene
    # una data/ora/nome reali, quindi la componiamo in modo deterministico
    # (format_helpers) invece di farla scrivere ad AI#2 - stesso motivo
    # per cui non le facciamo mai scrivere gli slot proposti.
    if response_type == ResponseType.CONFIRM_APPOINTMENT_TARGET and context.appointments:
        final_message = format_appointment_target_question(
            context.appointments[0], context.customer.full_name.value
        )
    else:
        # Solo i messaggi dell'UTENTE, mai le risposte precedenti del bot:
        # quelle potevano contenere liste di slot scritte in un turno precedente,
        # con il rischio che AI#2 le "riciclasse" invece di scrivere un testo
        # nuovo e corretto.
        history_text = "\n".join(
            m.text for m in context.memory.recent_messages[-6:] if m.role == "user"
        )
        ai2 = run_ai2_responder(
            response_type=response_type,
            system_result=system_result,
            message_text=message_text,
            history_text=history_text,
            knowledge_texts=knowledge_texts,
            operation_type=context.operation.type.value,
        )
        final_message = compose_final_message(ai2, context)

    # Il saluto orario è deterministico e va SOLO sul primo messaggio
    # della conversazione: l'AI non lo scrive mai, per non rischiare
    # incoerenze con l'ora reale o di ripeterlo ad ogni turno.
    if is_first_message:
        greeting = time_of_day_greeting(tenant.get("timezone"))
        final_message = f"{greeting}. {final_message}"

    context.memory.recent_messages.append(
        Message(
            role="assistant", text=final_message, timestamp=context.conversation.timeout.last_activity_at
        )
    )

    return context, final_message
