"""
INTENT ROUTER

Unico punto che decide "quale flow chiamare". Non contiene business
logic: fa solo il lookup nella tabella e invoca l'handler trovato. Se
la combinazione (step, intent) non è gestita, il default è esplicito
(non un comportamento indefinito).
"""

from __future__ import annotations

from app.context.models import ConversationContext, ConversationStep, SystemResult
from app.router.routing_table import ROUTING_TABLE


def _default_handler(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """
    Nessuna riga della tabella copre (step, intent) corrente. Non è un
    errore tecnico: è un caso non ancora previsto (o un messaggio fuori
    contesto). Segnaliamo esplicitamente ad AI#2 di chiedere chiarimento,
    invece di lasciare lo stato invariato in modo silenzioso.
    """
    return context, SystemResult(
        success=False,
        error_code="UNHANDLED_STATE",
        data={"step": context.conversation.current_step, "intent": context.conversation.current_intent},
    )


def route(context: ConversationContext, **flow_kwargs) -> tuple[ConversationContext, SystemResult]:
    key = (context.conversation.current_step, context.conversation.current_intent)
    handler = ROUTING_TABLE.get(key, _default_handler)
    return handler(context, **flow_kwargs)
